"""Creating a git-private project: the checkout must be private wherever it can push.

`tl project create NAME --git-checkout ABS_PATH` is the only way to make one; the
MCP server cannot. Before the registry entry is written:

  checkout   an absolute path to the top level of a git work tree, stored resolved.
             A checkout that is, contains, or sits inside another project's checkout
             is refused, so two projects do not own overlapping dataset folders.
             So is a checkout that is, contains, or sits inside the service home.
             `Registry.create_project` checks both under the registry lock,
             comparing folder names as the Mac's filesystem does.
  remotes    every fetch and push URL of every remote, after `insteadOf` and
             `pushInsteadOf` rewriting, must be a repository that
             `gh repo view github.com/<owner>/<repo> --json visibility` reports
             PRIVATE. PUBLIC, INTERNAL, a host other than github.com, a local path,
             a gh failure, and a checkout with no remote at all are refused. The
             public verbatim code repository therefore fails.
  push       a plain `git push` goes where `branch.<b>.pushRemote`,
             `remote.pushDefault` or `branch.<b>.remote` say. Each must name one of
             the checked remotes (or `.`, the checkout itself); a URL there is
             refused, and so are legacy `.git/remotes` and `.git/branches` files.
  branch     the checkout's current branch unless one is named; a detached HEAD
             without a named branch is refused.
  writers    the checkout's own repositories, the creating agent's repository, and
             any extra writer.
  commits    `git.commit` is on only when the checkout carries the sync tool's
             `tl.syncLock` marker. Without it the service saves files and commits
             nothing (see gitcommit.py).

Tests replace `visibility` with a function, so a local bare mirror can stand in for
a private GitHub repository. Nothing outside Python can pass that override.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from .gitcommit import GIT, CommitRefused, IndexLocked, git, marker
from .registry import Registry

#: owner/repo on github.com, from the URL forms git and gh write.
GITHUB_URL = re.compile(r"(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)"
                        r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")


def redact(url: str) -> str:
    """A URL without its `user:password@` part, for messages; a password may itself hold `@`."""
    return re.sub(r"(://)[^/]*@", r"\1", url)


def github_slug(url: str) -> str | None:
    m = GITHUB_URL.fullmatch(url.strip())
    return m["slug"] if m else None


def gh_visibility(url: str, gh_bin: str = "gh") -> str:
    """`PRIVATE`, `PUBLIC` or `INTERNAL` as gh reports it, or `UNKNOWN (<why>)`."""
    slug = github_slug(url)
    if slug is None:
        return "UNKNOWN (not a github.com repository)"
    try:
        r = subprocess.run([gh_bin, "repo", "view", f"github.com/{slug}", "--json", "visibility", "-q",
                            ".visibility"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"UNKNOWN (gh failed: {exc})"
    if r.returncode != 0 or not r.stdout.strip():
        return f"UNKNOWN (gh exited {r.returncode}: {r.stderr.strip()[:200]})"
    return r.stdout.strip()


def remote_urls(checkout: Path, git_bin: str = GIT) -> dict[str, list[str]]:
    """Every remote's fetch and push URLs, as git will use them."""
    out = {}
    for name in git(checkout, "remote", git_bin=git_bin).stdout.splitlines():
        urls = git(checkout, "remote", "get-url", "--all", name, git_bin=git_bin).stdout.split("\n")
        urls += git(checkout, "remote", "get-url", "--push", "--all", name, git_bin=git_bin).stdout.split("\n")
        out[name] = sorted({u for u in urls if u})
    return out


def push_targets(checkout: Path, remotes: dict, git_bin: str = GIT) -> list[str]:
    """Problems with where a plain `git push` could go: config values that are not remotes, legacy files."""
    problems = []
    r = git(checkout, "config", "--get-regexp", r"^(remote\.pushdefault|branch\..*\.(pushremote|remote))$",
            check=False, git_bin=git_bin)
    if r.returncode not in (0, 1):  # 1 means no such setting; anything else is a failure
        raise CommitRefused(f"git config --get-regexp failed: {r.stderr.strip()[:300]}")
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value != "." and value not in remotes:
            problems.append(f"{key} {redact(value)}: names no checked remote")
    common = checkout / git(checkout, "rev-parse", "--git-common-dir", git_bin=git_bin).stdout.strip()
    for legacy in ("remotes", "branches"):
        folder = common / legacy
        if folder.is_dir() and any(folder.iterdir()):
            problems.append(f"{folder}: legacy remote files are not checked")
    return problems


def own_repo(path: Path, git_bin: str = GIT) -> str | None:
    """The `owner/repo` of a clone's origin, for naming the creating agent's repository."""
    r = git(Path(path), "remote", "get-url", "origin", check=False, git_bin=git_bin)
    return github_slug(r.stdout) if r.returncode == 0 else None


def check_checkout(checkout: str | Path, visibility: Callable[[str], str] = gh_visibility,
                   git_bin: str = GIT) -> tuple[Path, list[str]]:
    """The resolved checkout and its GitHub repositories, or SystemExit naming what failed."""
    try:
        return _check_checkout(checkout, visibility, git_bin)
    except (CommitRefused, IndexLocked) as exc:
        raise SystemExit(f"REFUSING: {checkout}: {exc}")


def _check_checkout(checkout, visibility, git_bin):
    if not Path(checkout).is_absolute():
        raise SystemExit(f"REFUSING: the checkout {str(checkout)!r} must be an absolute path")
    path = Path(checkout).resolve()
    top = git(path, "rev-parse", "--show-toplevel", check=False, git_bin=git_bin) if path.is_dir() else None
    if top is None or top.returncode != 0:
        raise SystemExit(f"REFUSING: {path} is not a git checkout")
    if Path(top.stdout.strip()).resolve() != path:
        raise SystemExit(f"REFUSING: {path} is inside the checkout {top.stdout.strip()}; name its top level")
    remotes = remote_urls(path, git_bin)
    if not remotes:
        raise SystemExit(f"REFUSING: {path} has no remote; a git-private project needs a private GitHub "
                         "repository, or use a local-only project")
    bad = [f"{name} {redact(url)}: {vis}" for name, urls in sorted(remotes.items()) for url in urls
           if (vis := visibility(url)) != "PRIVATE"]
    push = push_targets(path, remotes, git_bin)
    parts = []
    if bad:
        parts.append("every remote of a git-private checkout must be a private repository; " + "; ".join(bad))
    if push:
        parts.append("a plain git push must go only to those remotes; " + "; ".join(push))
    if parts:
        raise SystemExit("REFUSING: " + ". Also, ".join(parts))
    slugs = sorted({s for urls in remotes.values() for u in urls if (s := github_slug(u))})
    return path, slugs


def create_git_project(registry: Registry, name: str, checkout: str | Path, *, creator_repo: str | None,
                       extra_writers: list[str] = (), branch: str | None = None, title: str = "",
                       visibility: Callable[[str], str] = gh_visibility, git_bin: str = GIT) -> dict:
    try:
        return _create_git_project(registry, name, checkout, creator_repo, extra_writers, branch, title,
                                   visibility, git_bin)
    except (CommitRefused, IndexLocked) as exc:  # a git failure or timeout is a refusal
        raise SystemExit(f"REFUSING: {checkout}: {exc}")


def _create_git_project(registry, name, checkout, creator_repo, extra_writers, branch, title, visibility,
                        git_bin):
    path, slugs = check_checkout(checkout, visibility, git_bin)
    if branch is None:
        r = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False, git_bin=git_bin)
        if r.returncode != 0 or not r.stdout.strip():
            raise SystemExit(f"REFUSING: {path} has a detached HEAD; name the branch the service commits on")
        branch = r.stdout.strip()
    writers = sorted({*slugs, *([creator_repo] if creator_repo else []), *extra_writers})
    return registry.create_project(name, privacy="git-private", writers=writers, title=title,
                                   git={"checkout": str(path), "branch": branch,
                                        "commit": marker(path, git_bin) is not None})
