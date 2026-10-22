"""The service commits a git-private dataset's label files itself, and never pushes.

It commits only in a checkout whose own sync workflow opted in to a shared lock,
because committing beside an unlocked rollback can lose labels: verbatim's
`data_sync.py` records HEAD at the start of a pull or push and resets to it on any
refusal, which would drop a label commit that landed in between.

  The lock    `flock` on `<absolute git dir>/tl-sync.flock`, which the checkout's
              sync tool holds for each pull, push and bootstrap. The name avoids
              `.lock`, because agents delete stale `.git/*.lock` files. The path
              comes from `git rev-parse --absolute-git-dir`; if it cannot be
              computed, nothing is committed.
  The opt-in  `git config tl.syncLock <version>` in the checkout's own config
              (`--local`; a global or system value does not count), written by the
              sync tool when it gains the lock. Without it the service saves files
              and commits nothing, and status says so. It is checked on every
              attempt, and again once the lock is held.
  The commit  `git commit --only -m <message> -- <paths>` of the dataset's existing
              label files (answers, review.json), after `git add` of a file not yet
              tracked. Status reads run with GIT_OPTIONAL_LOCKS=0. It never uses
              --no-verify, never pushes and never touches another path. On failure
              it unstages only its own paths.
  Refusals    a checkout that is not the top level of its work tree, HEAD off the
              configured branch, a rebase, merge, cherry-pick or revert in progress,
              anything else staged, a conflict on the paths, or no label file at all.
  Timeouts    every git call is stopped after GIT_TIMEOUT_S: SIGTERM to its process
              group, so git removes its own lock files and hooks can clean up; up
              to 10 s for the group to exit; then SIGKILL to whatever is left.
  Dying       the git calls that write (add, commit, reset) run under a small
              holder process that inherits the sync lock's descriptor and waits
              for git, without passing the descriptor to git. If launchd kills the
              service mid-commit, the holder keeps the lock until git exits, so the
              sync tool never sees a commit land during its own pull or push; a
              hook's background job or git's fsmonitor daemon never holds it. The
              cost: a git that hangs after the service died holds the lock, with no
              timeout, until someone stops it. `lsof <git dir>/tl-sync.flock` names
              the holder, which leads its own process group: stop the whole group
              (`kill -TERM -- -<pid>`), never the holder alone, or git would go on
              without the lock.
  Retries     a held index.lock is retried 5 times with backoff over about 2
              minutes, then waits 10 minutes; a refusal waits 10 minutes. A new
              save ends a 10-minute wait.
  Already in  if another agent's commit already holds the files' current bytes, git
              reports nothing to commit, and that counts as success.

Known gap: an agent that runs `git commit --amend` in the checkout right after a
label commit folds the labels into its own commit. Nothing is lost, but the message
no longer says what it holds.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__

GIT = "/usr/bin/git"
LOCK_NAME = "tl-sync.flock"
MARKER = "tl.syncLock"
INDEX_LOCK_RETRIES = 5
BACKOFF_S = (2, 5, 10, 30, 60)  # about two minutes in all
GIT_TIMEOUT_S = 120.0  # a commit's hooks included; longer is treated as hung
STOP_GRACE_S = 10.0  # after SIGTERM, how long a git process group may take to exit
#: Holds the sync lock (inherited from the service) while git runs as its child.
#: subprocess closes every other descriptor in the child, so git never has the lock.
HOLDER = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)"


class CommitRefused(Exception):
    """A precondition failed; the reason is safe to show in status."""


class IndexLocked(Exception):
    """git's index.lock is held by someone else."""


#: Variables that point git at another repository, index or `git -c` config, as a git
#: hook or `submodule foreach` exports them. The service always means the checkout it
#: names, so every call drops them. User-level config (GIT_CONFIG_GLOBAL) is kept,
#: because commits need the owner's identity from it.
SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CONFIG",
                "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def _env() -> dict:
    # Without GIT_CONFIG_COUNT, git ignores any GIT_CONFIG_KEY_<n> and GIT_CONFIG_VALUE_<n>.
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    return {**env, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def git(checkout: Path, *args: str, check: bool = True, git_bin: str = GIT,
        timeout: float | None = None, lock_fd: int | None = None) -> subprocess.CompletedProcess:
    """Run git. Output goes to temp files, not pipes, so a hook left running cannot block the read.

    With `lock_fd`, the sync lock's descriptor, git runs under HOLDER, which keeps the
    lock for as long as git runs even if this process dies, and gives it to no one else.
    """
    timeout = GIT_TIMEOUT_S if timeout is None else timeout
    cmd = [git_bin, "-C", str(checkout), *args]
    argv = cmd if lock_fd is None else [sys.executable, "-I", "-c", HOLDER, *cmd]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=_env(),
                                start_new_session=True, pass_fds=() if lock_fd is None else (lock_fd,))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise CommitRefused(f"git {' '.join(args[:2])} ran longer than {timeout:g} s and was stopped")
        out.seek(0)
        err.seek(0)
        r = subprocess.CompletedProcess(cmd, proc.returncode, out.read().decode(errors="replace"),
                                        err.read().decode(errors="replace"))
    if check and r.returncode != 0:
        if "index.lock" in r.stderr:
            raise IndexLocked(r.stderr.strip())
        raise CommitRefused(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM the process group (git removes its lock files on it, hooks may clean up),
    wait up to STOP_GRACE_S for the whole group to exit, then SIGKILL what is left."""
    _killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_S
    while time.monotonic() < deadline:
        proc.poll()  # reap the group's leader, so it does not linger as a zombie
        if not _group_alive(proc.pid):
            break
        time.sleep(0.05)
    _killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):  # gone, or (macOS: EPERM) only zombies left
        return False
    return True


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # gone, or (macOS: EPERM) only zombies left
        pass


def absolute_git_dir(checkout: Path, git_bin: str = GIT) -> Path:
    r = git(checkout, "rev-parse", "--absolute-git-dir", check=False, git_bin=git_bin)
    path = Path(r.stdout.strip())
    if r.returncode != 0 or not r.stdout.strip() or not path.is_absolute() or not path.is_dir():
        raise CommitRefused(f"cannot find the git dir of {checkout}; nothing is committed")
    return path


def marker(checkout: Path, git_bin: str = GIT) -> str | None:
    """The sync tool's opt-in version, or None when the checkout has not opted in."""
    r = git(checkout, "config", "--local", "--get", MARKER, check=False, git_bin=git_bin)
    return r.stdout.strip() or None if r.returncode == 0 else None


def preconditions(checkout: Path, branch: str, paths: list[str], git_dir: Path, git_bin: str = GIT) -> None:
    top = git(checkout, "rev-parse", "--show-toplevel", git_bin=git_bin).stdout.strip()
    if Path(top).resolve() != checkout.resolve():
        raise CommitRefused(f"{checkout} is not the top level of its work tree ({top}); nothing is committed")
    head = git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False, git_bin=git_bin).stdout.strip()
    if head != branch:
        raise CommitRefused(f"HEAD is on {head or 'a detached commit'}, not {branch}")
    for marker_file, what in (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"), ("MERGE_HEAD", "a merge"),
                              ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert")):
        if (git_dir / marker_file).exists():
            raise CommitRefused(f"{what} is in progress")
    staged = git(checkout, "diff", "--cached", "--name-only", "-z", git_bin=git_bin).stdout.split("\0")
    foreign = sorted(p for p in staged if p and p not in paths)
    if foreign:
        raise CommitRefused(f"other changes are staged ({foreign[:3]}); the label commit waits for them")
    conflicts = git(checkout, "diff", "--name-only", "--diff-filter=U", "-z", git_bin=git_bin).stdout.split("\0")
    if set(paths) & {p for p in conflicts if p}:
        raise CommitRefused("a label file is in conflict")


def head_holds(checkout: Path, paths: list[str], git_bin: str = GIT) -> bool:
    """Whether HEAD already holds every path's current bytes."""
    for p in paths:
        blob = git(checkout, "hash-object", "--", p, git_bin=git_bin).stdout.strip()
        r = git(checkout, "rev-parse", "--verify", "--quiet", f"HEAD:{p}", check=False, git_bin=git_bin)
        if r.returncode != 0 or r.stdout.strip() != blob:
            return False
    return True


def commit_once(checkout: Path, branch: str, files: list[Path], message: str, *, wait_s: float | None = None,
                git_bin: str = GIT) -> dict:
    """Take the shared lock, check, and commit those of `files` that exist.

    Returns `{"committed": True, "sha": ...}`, or `{"committed": False, "already": True}`
    when HEAD already holds the files' bytes. `wait_s` bounds the wait for the sync lock;
    None waits as long as it takes. Raises CommitRefused or IndexLocked.
    """
    checkout = Path(checkout).resolve()
    if not marker(checkout, git_bin):
        raise CommitRefused(f"{checkout} has not opted in (`git config --local {MARKER}` is unset); labels "
                            "are saved, not committed")
    git_dir = absolute_git_dir(checkout, git_bin)
    # Resolve each file's folder, not the file, so /tmp and /private/tmp spellings agree.
    full = [Path(f).parent.resolve() / Path(f).name for f in files]
    outside = [str(f) for f in full if not f.is_relative_to(checkout)]
    if outside:
        raise CommitRefused(f"label files {outside} are outside {checkout}")
    paths = [str(f.relative_to(checkout)) for f in full if f.exists()]
    if not paths:
        raise CommitRefused(f"none of the label files {[f.name for f in full]} exists; nothing to commit")
    with open(git_dir / LOCK_NAME, "a") as fh:
        if wait_s is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CommitRefused("the checkout's sync lock is held; the commit waits")
                    time.sleep(0.05)
        try:
            if not marker(checkout, git_bin):  # re-check under the lock
                raise CommitRefused(f"{checkout} has not opted in; labels are saved, not committed")
            preconditions(checkout, branch, paths, git_dir, git_bin)
            untracked = [p for p in paths
                         if git(checkout, "ls-files", "--error-unmatch", "--", p, check=False,
                                git_bin=git_bin).returncode != 0]
            try:
                if untracked:
                    git(checkout, "add", "--", *untracked, git_bin=git_bin, lock_fd=fh.fileno())
                r = git(checkout, "commit", "--only", "-q", "-m", message, "--", *paths, check=False,
                        git_bin=git_bin, lock_fd=fh.fileno())
                if r.returncode != 0:
                    if "index.lock" in r.stderr:
                        raise IndexLocked(r.stderr.strip())
                    if head_holds(checkout, paths, git_bin):
                        return {"committed": False, "already": True}
                    raise CommitRefused(f"git commit failed: {(r.stderr or r.stdout).strip()[:300]}")
            except (CommitRefused, IndexLocked):
                # Unstage only our own paths; never another agent's work.
                git(checkout, "reset", "-q", "--", *paths, check=False, git_bin=git_bin, lock_fd=fh.fileno())
                raise
            sha = git(checkout, "rev-parse", "HEAD", git_bin=git_bin).stdout.strip()
            return {"committed": True, "sha": sha}
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def commit_message(project: str, dataset: str, saves: int) -> str:
    return f"Save human labels: {project}/{dataset} ({saves} save{'s' if saves != 1 else ''}, tl {__version__})"


@dataclass
class Pending:
    """One dataset's commit state, as `tl status` shows it."""
    first_save: float | None = None
    last_save: float | None = None
    saves: int = 0
    finished: bool = False
    next_try: float = 0.0
    tries: int = 0
    last_error: str | None = None
    last_commit: str | None = None


class Committer:
    """Decides when a dataset's labels are committed: 300 s after the last save, at least
    every 1800 s during a long sitting, and at once on Finish (the registry's `idle_s` and
    `max_age_s`). The service calls `note_save` after each browser write and `tick` from
    a background thread; the git work happens in `tick`, never in a request.

    A save that lands while a commit runs stays pending, because the commit may have
    read the file before it. Waiting for the sync lock is capped at SYNC_WAIT_S, so one
    stuck checkout delays other projects' commits by at most that long per tick. Any
    error in one dataset, even an unexpected one, is recorded as its `last_error` with
    a 10-minute wait, and the tick goes on to the next dataset. Only one tick runs at a
    time; a second call while one runs returns one result with `skipped` set, at once.
    """

    SYNC_WAIT_S = 900.0  # a sync longer than this is retried after the 600 s refusal wait

    def __init__(self, registry, clock=time.monotonic, git_bin: str = GIT, commit=commit_once):
        self.registry = registry
        self.clock = clock
        self.git_bin = git_bin
        self.commit = commit
        self.state: dict[tuple[str, str], Pending] = {}
        self._lock = threading.Lock()
        self._ticking = threading.Lock()

    def note_save(self, project: str, dataset: str, finished: bool = False) -> None:
        entry = self.registry.project(project)
        if entry["privacy"] != "git-private" or not entry["git"].get("commit"):
            return
        with self._lock:
            st = self.state.setdefault((project, dataset), Pending())
            now = self.clock()
            st.first_save = st.first_save if st.first_save is not None else now
            st.last_save = now
            st.saves += 1
            st.finished = st.finished or finished
            if st.next_try - now > BACKOFF_S[-1]:
                st.next_try = 0.0  # a save ends a 10-minute wait; the backoff itself still runs

    def _due(self, git: dict, st: Pending, now: float) -> bool:
        if not st.saves or now < st.next_try:
            return False
        return st.finished or now - st.last_save >= git["idle_s"] or now - st.first_save >= git["max_age_s"]

    def tick(self, files_of) -> list[dict]:
        """Commit every dataset that is due. `files_of(project, dataset)` gives its label files."""
        if not self._ticking.acquire(blocking=False):
            return [{"project": None, "dataset": None, "skipped": "another tick is still running"}]
        try:
            return self._tick(files_of)
        finally:
            self._ticking.release()

    def _tick(self, files_of) -> list[dict]:
        results = []
        projects = self.registry.load()["projects"]
        with self._lock:
            keys = list(self.state)
        for key in keys:
            project, dataset = key
            entry = projects.get(project)
            if entry is None or entry["privacy"] != "git-private" or not entry["git"].get("commit"):
                with self._lock:
                    self.state.pop(key, None)
                results.append({"project": project, "dataset": dataset, "dropped": True,
                                "error": "the project is gone or no longer commits"})
                continue
            git = entry["git"]
            with self._lock:
                st = self.state[key]
                started = self.clock()
                if not self._due(git, st, started):
                    continue
                snap_saves = st.saves
            try:
                res = self.commit(Path(git["checkout"]), git["branch"], files_of(project, dataset),
                                  commit_message(project, dataset, snap_saves), wait_s=self.SYNC_WAIT_S,
                                  git_bin=self.git_bin)
            except IndexLocked as exc:
                with self._lock:
                    st.tries += 1
                    st.last_error = str(exc)[:300]
                    st.next_try = self.clock() + (BACKOFF_S[st.tries - 1] if st.tries <= INDEX_LOCK_RETRIES else 600)
                results.append({"project": project, "dataset": dataset, "pending": True, "error": st.last_error})
                continue
            except (Exception, SystemExit) as exc:  # one dataset's failure never stops the others
                with self._lock:
                    st.last_error = str(exc) if isinstance(exc, CommitRefused) else f"{type(exc).__name__}: {exc}"
                    st.next_try = self.clock() + 600
                results.append({"project": project, "dataset": dataset, "pending": True, "error": st.last_error})
                continue
            with self._lock:
                st.last_commit = res.get("sha") or ("already committed" if res.get("already") else None)
                st.tries, st.last_error, st.next_try = 0, None, 0.0
                st.saves -= snap_saves
                if st.saves:  # saved during the commit: keep them pending, counted from the commit's start
                    st.first_save = started
                else:
                    st.first_save = st.last_save = None
                    st.finished = False
            results.append({"project": project, "dataset": dataset, **res})
        return results

    def status(self, project: str, dataset: str) -> dict:
        entry = self.registry.project(project)
        if entry["privacy"] != "git-private":
            return {"commits": "not a git-private project"}
        if not entry["git"].get("commit"):
            return {"commits": "off for this project; labels are saved, not committed"}
        with self._lock:
            st = self.state.get((project, dataset), Pending())
            out = {"pending_saves": st.saves, "last_error": st.last_error, "last_commit": st.last_commit}
        opted = marker(Path(entry["git"]["checkout"]), self.git_bin)
        return {"commits": "on" if opted else "off: the checkout has not opted in to the sync lock", **out}
