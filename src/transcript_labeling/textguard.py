"""Transcript text never lands in a git work tree by accident.

`tl export` (and anything else that prints transcript text) calls
`check_text_target` before writing. It refuses when the destination is a file
inside a git work tree, unless the caller passed `--no-text`:

  --out PATH         PATH is resolved first (symlinks, including one to a file not
                     made yet, and `..`, as the OS will apply them), then its
                     nearest existing folder is checked. `--out -` means stdout.
  redirected stdout  a regular file on stdout is found with macOS
                     `fcntl(F_GETPATH)` and checked the same way. If its path
                     cannot be found or checked, the text is refused.
  terminal or pipe   allowed. A pipe such as `| tee file` gets past this check;
                     it is a second line of defence, and the skill states the rule.

"Inside a git work tree" means some folder from the target up to `/` holds a
`.git` entry, a folder (a clone) or a file (a worktree or submodule). A
gitignored path counts too: ignored files can still be force-added, and the
folder may be synced elsewhere.

Known limits: a file reached through a hard link from outside a repo is allowed,
and so is a work tree with no `.git` entry of its own (a bare repository used with
`--work-tree`, `GIT_DIR` or `core.worktree`).
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path

MAXPATHLEN = 1024


def _exists(path: Path) -> bool:
    """Whether `path` exists. Only "no such file" counts as absent; any other error
    (a folder you may not enter, a symlink loop) raises, the same on every Python."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def work_tree_of(path: str | Path) -> Path | None:
    """The top folder of the git work tree holding `path`, or None. Raises OSError when
    the path cannot be checked."""
    # realpath follows a symlink whose target does not exist yet, and applies `..` the
    # way the OS will when the file is written; errors are left to _exists.
    p = Path(os.path.realpath(path))
    while not _exists(p):
        p = p.parent
    p = p.resolve(strict=True)  # it exists by now; strict only matters if it vanishes in between
    for folder in (p, *p.parents) if p.is_dir() else p.parents:
        if _exists(folder / ".git"):
            return folder
    return None


def stdout_path(fd: int = 1) -> Path | None:
    """The file behind `fd` when it is a regular file; None for a terminal, pipe or socket.

    Raises OSError when `fd` is a regular file whose path the OS will not give.
    """
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return None
    if not hasattr(fcntl, "F_GETPATH"):
        raise OSError("this platform has no F_GETPATH, so a redirected stdout cannot be checked")
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(MAXPATHLEN))
    except OSError as exc:
        if exc.errno == errno.ENOSPC:  # how F_GETPATH reports a buffer too small for the path
            raise OSError(f"its path does not fit in {MAXPATHLEN} bytes") from exc
        raise
    return Path(os.fsdecode(raw.split(b"\0", 1)[0]))


def destination(out: str | Path | None) -> Path | None:
    """Where text for `out` goes: a file, or None for stdout (no `--out`, or `--out -`)."""
    return None if out is None or str(out) == "-" else Path(out)


def check_text_target(out: str | Path | None, no_text: bool, stdout_fd: int = 1) -> Path | None:
    """Refuse (SystemExit) transcript text bound for a file inside a git work tree.

    Returns `destination(out)`, the place it checked; the caller writes there, so the
    guard and the writer never disagree about what `-` means. The caller must open
    that path in place. A temp-file-and-rename writer such as `store.write_atomic`
    would replace a symlink inside a repo that points outside it with a regular file
    in the repo, which this check allows because the link's target is outside.
    """
    dest = destination(out)
    if no_text:
        return dest
    if dest is not None:
        target, how = dest, "--out"
    else:
        try:
            target, how = stdout_path(stdout_fd), "stdout"
        except OSError as exc:
            raise SystemExit(f"REFUSING: stdout is a file whose path cannot be checked ({exc}); "
                             "write to a path with --out, or pass --no-text")
        if target is None:
            return dest
    try:
        tree = work_tree_of(target)
    except OSError as exc:
        raise SystemExit(f"REFUSING: {how} {target} cannot be checked ({exc}); pass --no-text or another path")
    if tree is not None:
        raise SystemExit(f"REFUSING: {how} {target} is inside the git work tree {tree}, and transcript text "
                         "never enters a repository. Pass --no-text, or write outside any checkout "
                         "(the default is $TL_HOME/exports/).")
    return dest
